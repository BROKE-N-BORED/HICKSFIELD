"""Tests for the v0.5.0 update check and verified install.

A fake GitHub server (localhost, ephemeral port) serves the
releases endpoint and the tarball: no test ever touches the real
api.github.com. The tarballs are built in the test from real
tree files, so the verify and install paths run for real.
"""

import io
import json
import os
import shutil
import sys
import tarfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hicksfield import updates
from hicksfield.errors import HicksFieldError
from tests.helpers import TempEnvTestCase

TREE = Path(__file__).resolve().parents[1]
SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
TOP = "HICKSFIELD-%s" % SHA


def make_tarball(top, files):
    """A tarball (bytes) with `top` as its one folder. files maps
    a path inside the folder to bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for rel, content in sorted(files.items()):
            data = content if isinstance(content, bytes) else content.encode()
            info = tarfile.TarInfo("%s/%s" % (top, rel))
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def release_payload(**overrides):
    payload = {
        "tag_name": "v0.7.0",
        "target_commitish": SHA,
        "tarball_url": "http://127.0.0.1:1/tarball",
        "html_url": "https://github.com/BROKE-N-BORED/HICKSFIELD/releases/tag/v0.7.0",
        "body": "The update release.",
        "draft": False,
        "prerelease": False,
    }
    payload.update(overrides)
    return payload


class FakeGitHubHandler(BaseHTTPRequestHandler):
    payload = None
    tarball = b""
    status = 200
    raw_body = None
    requests = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        FakeGitHubHandler.requests += 1
        if self.path == "/releases/latest":
            if self.raw_body is not None:
                body = self.raw_body
            else:
                body = json.dumps(self.payload).encode()
            self._send(self.status, body, "application/json")
        elif self.path == "/tarball":
            self._send(200, self.tarball, "application/gzip")
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeServerTestCase(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeGitHubHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        FakeGitHubHandler.payload = release_payload(
            tarball_url=self.tarball_url())
        FakeGitHubHandler.status = 200
        FakeGitHubHandler.raw_body = None
        FakeGitHubHandler.tarball = b""
        FakeGitHubHandler.requests = 0

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def api_url(self):
        return "http://127.0.0.1:%d/releases/latest" % self.port

    def tarball_url(self):
        return "http://127.0.0.1:%d/tarball" % self.port


class VersionTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(updates.parse_version("0.5.0"), (0, 5, 0))
        self.assertEqual(updates.parse_version("v0.5.0"), (0, 5, 0))
        self.assertEqual(updates.parse_version("0.5"), (0, 5))
        self.assertEqual(updates.parse_version("  v1.2  "), (1, 2))
        self.assertIsNone(updates.parse_version("v0.5-beta"))
        self.assertIsNone(updates.parse_version("latest"))
        self.assertIsNone(updates.parse_version(""))
        self.assertIsNone(updates.parse_version(None))
        self.assertIsNone(updates.parse_version(5))

    def test_is_newer(self):
        self.assertTrue(updates.is_newer("0.6.0", "0.5.0"))
        self.assertTrue(updates.is_newer("v0.6", "0.5.0"))
        self.assertFalse(updates.is_newer("0.5.0", "0.5.0"))
        self.assertFalse(updates.is_newer("0.5.0", "0.5"))
        self.assertFalse(updates.is_newer("0.4.0", "0.5.0"))
        self.assertFalse(updates.is_newer("latest", "0.5.0"))
        self.assertFalse(updates.is_newer("0.6.0", "dev"))


class CheckTests(FakeServerTestCase):
    def test_newer_release(self):
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "newer")
        self.assertEqual(result["latest"], "0.7.0")
        self.assertEqual(result["sha"], SHA)
        self.assertEqual(result["tarball_url"], self.tarball_url())
        self.assertIn("update release", result["body"])

    def test_current_release(self):
        FakeGitHubHandler.payload = release_payload(
            tag_name="v0.5.0", tarball_url=self.tarball_url())
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["latest"], "0.5.0")

    def test_dev_tree_newer_than_release_is_current(self):
        result = updates.check_for_update("0.7.0", url=self.api_url())
        self.assertEqual(result["state"], "current")

    def test_tag_without_v_prefix(self):
        FakeGitHubHandler.payload = release_payload(
            tag_name="0.6.0", tarball_url=self.tarball_url())
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "newer")

    def test_malformed_tag_is_unknown(self):
        FakeGitHubHandler.payload = release_payload(
            tag_name="nightly", tarball_url=self.tarball_url())
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "unknown")
        self.assertIn("did not look like a version", result["reason"])

    def test_prerelease_is_unknown(self):
        FakeGitHubHandler.payload = release_payload(
            prerelease=True, tarball_url=self.tarball_url())
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "unknown")
        self.assertIn("not a finished release", result["reason"])

    def test_draft_is_unknown(self):
        FakeGitHubHandler.payload = release_payload(
            draft=True, tarball_url=self.tarball_url())
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "unknown")

    def test_missing_commit_is_unknown(self):
        FakeGitHubHandler.payload = release_payload(
            target_commitish="", tarball_url=self.tarball_url())
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "unknown")
        self.assertIn("commit", result["reason"])

    def test_server_error_is_unknown(self):
        FakeGitHubHandler.status = 500
        FakeGitHubHandler.raw_body = b"boom"
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "unknown")

    def test_bad_json_is_unknown(self):
        FakeGitHubHandler.raw_body = b"not json at all"
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "unknown")

    def test_unreachable_server_is_unknown(self):
        result = updates.check_for_update(
            "0.5.0", url="http://127.0.0.1:1/releases/latest")
        self.assertEqual(result["state"], "unknown")
        self.assertIn("Could not reach GitHub", result["reason"])
        # No raw errno at the user: the reason is plain words.
        self.assertNotIn("Errno", result["reason"])

    def test_urlopen_stub_seam(self):
        payload = json.dumps(release_payload(
            tarball_url=self.tarball_url())).encode()

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return payload

        result = updates.check_for_update(
            "0.5.0", urlopen=lambda *a, **k: _Response())
        self.assertEqual(result["state"], "newer")

    def test_env_redirects_the_endpoint(self):
        with mock.patch.dict(os.environ, {"HICKSFIELD_UPDATE_API_URL":
                                          self.api_url()}):
            result = updates.check_for_update("0.5.0")
        self.assertEqual(result["state"], "newer")


class VerifyTests(TempEnvTestCase):
    def _write(self, data, name="update.tar.gz"):
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def _good_files(self):
        return {
            "hicksfield/__init__.py": "__version__ = \"0.6.0\"\n",
            "hicksfield/__main__.py": "",
            "run.sh": "#!/bin/sh\n",
            "installer.py": "",
        }

    def test_good_tarball_verifies(self):
        path = self._write(make_tarball(TOP, self._good_files()))
        self.assertEqual(updates.verify_tarball(path, SHA), TOP)

    def test_wrong_commit_refused(self):
        other = "HICKSFIELD-%s" % ("0" * 40)
        path = self._write(make_tarball(other, self._good_files()))
        with self.assertRaises(HicksFieldError) as caught:
            updates.verify_tarball(path, SHA)
        self.assertIn("did not match the release",
                      str(caught.exception))

    def test_two_top_folders_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for top in (TOP, "SOMETHING-ELSE"):
                data = b"x"
                info = tarfile.TarInfo("%s/file.txt" % top)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        path = self._write(buffer.getvalue())
        with self.assertRaises(HicksFieldError):
            updates.verify_tarball(path, SHA)

    def test_dotdot_member_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            data = b"x"
            info = tarfile.TarInfo("%s/../evil.txt" % TOP)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        path = self._write(buffer.getvalue())
        with self.assertRaises(HicksFieldError):
            updates.verify_tarball(path, SHA)

    def test_absolute_member_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            data = b"x"
            info = tarfile.TarInfo("/etc/evil.txt")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        path = self._write(buffer.getvalue())
        with self.assertRaises(HicksFieldError):
            updates.verify_tarball(path, SHA)

    def test_empty_tarball_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz"):
            pass
        path = self._write(buffer.getvalue())
        with self.assertRaises(HicksFieldError):
            updates.verify_tarball(path, SHA)

    def test_missing_package_refused(self):
        files = {"run.sh": "#!/bin/sh\n", "README.md": "hi\n"}
        path = self._write(make_tarball(TOP, files))
        with self.assertRaises(HicksFieldError):
            updates.verify_tarball(path, SHA)

    def test_not_a_tarball_refused(self):
        path = self._write(b"this is not a tarball")
        with self.assertRaises(HicksFieldError):
            updates.verify_tarball(path, SHA)


class InstallTests(FakeServerTestCase):
    def _fake_root(self):
        """A fake installed tree: real package files, a venv with
        a working python, one file the new version drops, and a
        venv sentinel that must survive."""
        root = self.tmp / "hicksfield-app"
        root.mkdir()
        shutil.copytree(TREE / "hicksfield", root / "hicksfield")
        shutil.copy(TREE / "run.sh", root / "run.sh")
        shutil.copy(TREE / "installer.py", root / "installer.py")
        (root / "OLDFILE.txt").write_text("dropped in 0.7.0\n")
        venv_bin = root / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(sys.executable)
        (root / "venv" / "sentinel.txt").write_text("keep me\n")
        return root

    def _new_tree_files(self):
        main = (TREE / "hicksfield" / "__main__.py").read_text()
        return {
            "hicksfield/__init__.py":
                "\"\"\"HicksField.\"\"\"\n\n__version__ = \"0.7.0\"\n",
            "hicksfield/__main__.py": main,
            "run.sh": (TREE / "run.sh").read_text(),
            "installer.py": (TREE / "installer.py").read_text(),
            "NEWFILE.txt": "new in 0.7.0\n",
        }

    def test_install_replaces_tree_and_keeps_venv(self):
        root = self._fake_root()
        tarball = self.tmp / "update.tar.gz"
        tarball.write_bytes(
            make_tarball(TOP, self._new_tree_files()))
        version = updates.install_update(tarball, SHA, "0.7.0",
                                         root=root)
        self.assertEqual(version, "0.7.0")
        self.assertIn("__version__ = \"0.7.0\"",
                      (root / "hicksfield" / "__init__.py").read_text())
        self.assertTrue((root / "NEWFILE.txt").exists())
        self.assertFalse((root / "OLDFILE.txt").exists())
        self.assertFalse((root / "hicksfield" / "stills.py").exists())
        self.assertTrue((root / "venv" / "sentinel.txt").exists())
        self.assertTrue((root / "venv" / "bin" / "python").exists())
        self.assertTrue(os.access(root / "run.sh", os.X_OK))

    def test_install_refuses_tampered_tarball_and_changes_nothing(self):
        root = self._fake_root()
        other = "HICKSFIELD-%s" % ("0" * 40)
        tarball = self.tmp / "tampered.tar.gz"
        tarball.write_bytes(
            make_tarball(other, self._new_tree_files()))
        before = (root / "hicksfield" / "__init__.py").read_text()
        with self.assertRaises(HicksFieldError) as caught:
            updates.install_update(tarball, SHA, "0.7.0", root=root)
        self.assertIn("did not match the release",
                      str(caught.exception))
        self.assertEqual(
            (root / "hicksfield" / "__init__.py").read_text(), before)
        self.assertTrue((root / "OLDFILE.txt").exists())
        self.assertFalse((root / "NEWFILE.txt").exists())

    def test_install_refuses_a_copy_without_a_venv(self):
        root = self.tmp / "plain-copy"
        root.mkdir()
        shutil.copytree(TREE / "hicksfield", root / "hicksfield")
        tarball = self.tmp / "update.tar.gz"
        tarball.write_bytes(
            make_tarball(TOP, self._new_tree_files()))
        with self.assertRaises(HicksFieldError) as caught:
            updates.install_update(tarball, SHA, "0.7.0", root=root)
        self.assertIn("not installed with the installer",
                      str(caught.exception))
        self.assertFalse((root / "NEWFILE.txt").exists())

    def test_download_and_install_through_the_fake_server(self):
        root = self._fake_root()
        FakeGitHubHandler.tarball = make_tarball(
            TOP, self._new_tree_files())
        result = updates.check_for_update("0.5.0", url=self.api_url())
        self.assertEqual(result["state"], "newer")
        dest = self.tmp / "downloaded.tar.gz"
        updates.download_release(result, dest)
        self.assertTrue(dest.exists())
        self.assertGreater(dest.stat().st_size, 0)
        version = updates.install_update(dest, result["sha"],
                                         result["latest"], root=root)
        self.assertEqual(version, "0.7.0")
        self.assertIn("__version__ = \"0.7.0\"",
                      (root / "hicksfield" / "__init__.py").read_text())

    def test_restart_replaces_the_process_with_the_app(self):
        seen = {}

        def _fake_execv(path, argv):
            seen["path"] = path
            seen["argv"] = argv
            raise RuntimeError("execv called")

        with mock.patch("os.execv", _fake_execv):
            with self.assertRaises(RuntimeError):
                updates.restart_app()
        self.assertEqual(seen["path"], sys.executable)
        self.assertEqual(seen["argv"][:3],
                         [sys.executable, "-m", "hicksfield"])


class GuiWiringTests(FakeServerTestCase):
    """The MainWindow wiring: Help menu items, the startup toggle,
    and the manual check both ways. The popup itself is covered
    by the GUI drive; here it is stubbed to record the call."""

    def setUp(self):
        super().setUp()
        from PySide6.QtCore import QSettings
        from PySide6.QtTest import QTest
        from tests.helpers import app, require_qt

        require_qt(self)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                          str(self.tmp / "qsettings"))
        # The app's settings are NativeFormat, which the IniFormat
        # path above does not redirect: they live in the real
        # config file for the run. Clear the one key these tests
        # own, so each test starts from the default (on) and
        # leaves no residue behind.
        from hicksfield.gui import settings as app_settings

        app_settings().remove("check_updates_on_startup")
        app_settings().sync()
        os.environ["HICKSFIELD_OLLAMA_URL"] = "http://127.0.0.1:1"
        os.environ["HICKSFIELD_UPDATE_API_URL"] = self.api_url()
        self.app = app()
        self._qtest = QTest
        from hicksfield.gui import AppState, MainWindow

        self.state = AppState()
        self.state.new_project("test film")
        self.main = MainWindow(self.state)
        self.shown = []

    def tearDown(self):
        self.main.close()
        self.main.deleteLater()
        self._qtest.qWait(200)
        from hicksfield.gui import settings as app_settings

        app_settings().remove("check_updates_on_startup")
        app_settings().sync()
        super().tearDown()

    def _help_actions(self):
        for action in self.main.menuBar().actions():
            if action.text() == "Help":
                return [sub.text()
                        for sub in action.menu().actions()]
        return []

    def _wait_idle(self):
        from tests.helpers import wait_for

        return wait_for(lambda: self.main._update_worker is None,
                        timeout_ms=10000)

    def test_help_menu_has_the_update_items(self):
        actions = self._help_actions()
        self.assertIn("Check for updates", actions)
        self.assertIn("Check for updates on startup", actions)
        self.assertTrue(self.main._auto_update_action.isChecked())

    def test_startup_check_is_on_by_default(self):
        self.assertTrue(self.main._load_update_check())

    def test_toggle_turns_the_startup_check_off_and_on(self):
        from hicksfield.gui import settings

        self.main._auto_update_action.trigger()
        self.assertFalse(self.main._load_update_check())
        self.assertEqual(
            settings().value("check_updates_on_startup", type=int), 0)
        self.main._auto_update_action.trigger()
        self.assertTrue(self.main._load_update_check())

    def test_manual_check_when_current_says_so(self):
        from tests.helpers import wait_for

        FakeGitHubHandler.payload = release_payload(
            tag_name="v0.6.0", tarball_url=self.tarball_url())
        self.main.show()
        self.assertTrue(self._wait_idle())
        self.main._on_check_updates()
        self.assertTrue(wait_for(
            lambda: "You have the latest version (0.6.0)."
            in self.main.status_label.text(),
            timeout_ms=10000))

    def test_manual_check_when_newer_offers_the_update(self):
        from tests.helpers import wait_for

        seen = []
        self.main._show_update_available = seen.append
        self.main.show()
        self.assertTrue(self._wait_idle())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["latest"], "0.7.0")
        seen.clear()
        self.main._on_check_updates()
        self.assertTrue(wait_for(lambda: len(seen) == 1,
                                 timeout_ms=10000))
        self.assertEqual(seen[0]["latest"], "0.7.0")

    def test_startup_check_off_makes_no_request(self):
        from hicksfield.gui import settings

        settings().setValue("check_updates_on_startup", 0)
        self.main.show()
        self._qtest.qWait(600)
        self.assertEqual(FakeGitHubHandler.requests, 0)

    def test_startup_check_on_makes_one_request(self):
        self.main._show_update_available = lambda result: None
        self.main.show()
        self.assertTrue(self._wait_idle())
        self.assertEqual(FakeGitHubHandler.requests, 1)


if __name__ == "__main__":
    unittest.main()
