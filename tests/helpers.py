"""Shared test helpers: temp app dirs, fixtures, Qt guard."""

import base64
import os
import tempfile
import unittest
import wave
from pathlib import Path

# A 1x1 red PNG, for still fixtures and ffmpeg tests.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

ENV_KEYS = ("HICKSFIELD_DATA_DIR", "HICKSFIELD_CONFIG_DIR",
            "HICKSFIELD_HOME", "HICKSFIELD_FASTSD_DIR",
            "HICKSFIELD_FASTSD_MODEL", "HICKSFIELD_SDCPP_DIR",
            "HICKSFIELD_SD_CLI", "HICKSFIELD_SD_MODEL",
            "HICKSFIELD_PIPER", "HICKSFIELD_VOICE_DIR",
            "HICKSFIELD_OLLAMA_URL")


class TempEnvTestCase(unittest.TestCase):
    """Redirects the app dirs to a temp tree for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {}
        for key in ENV_KEYS:
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        os.environ["HICKSFIELD_DATA_DIR"] = str(self.tmp / "data")
        os.environ["HICKSFIELD_CONFIG_DIR"] = str(self.tmp / "config")
        os.environ["HICKSFIELD_HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


def write_png(path, size=1):
    """Write a tiny PNG fixture. size is ignored (1x1) unless Qt is
    available, in which case a real square of that size is drawn."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if size <= 1:
        path.write_bytes(base64.b64decode(TINY_PNG_B64))
        return path
    try:
        from PySide6.QtGui import QImage
        from PySide6.QtCore import Qt
    except ImportError:
        path.write_bytes(base64.b64decode(TINY_PNG_B64))
        return path
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(Qt.red)
    image.save(str(path))
    return path


def write_wav(path, seconds=1.0, rate=22050):
    """Write a silent WAV of `seconds` duration."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return path


def qt_available():
    try:
        import PySide6  # noqa: F401
        return True
    except ImportError:
        return False


def require_qt(test_case):
    if not qt_available():
        test_case.skipTest("PySide6 not installed")


def app():
    import atexit
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
        atexit.register(_shutdown_qt)
    return instance


def _shutdown_qt():
    """Tear Qt down cleanly before the interpreter does.

    Join the workers, close the windows, and collect the wrappers so
    the C++ widgets die while the application still lives. The
    windows must actually be collectable: the app's signal slots use
    weak references for exactly this reason (see gui._weak_slot). No
    object is force-deleted here: shiboken6.delete() on a window
    segfaults this PySide6 build.
    """
    import gc
    import sys
    try:
        from hicksfield.workers import shutdown_workers
        shutdown_workers()
    except Exception:  # noqa: BLE001 - teardown must not raise
        pass
    try:
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        instance = QApplication.instance()
        if instance is not None:
            for widget in list(instance.topLevelWidgets()):
                try:
                    widget.close()
                    # Deferred destruction: signal connections keep
                    # closed windows alive, and only destroying them
                    # releases the connections. deleteLater is the
                    # safe way; shiboken6.delete() segfaults here.
                    widget.deleteLater()
                except RuntimeError:
                    pass
            for _ in range(10):
                instance.processEvents()
                QTest.qWait(50)
                if not instance.topLevelWidgets():
                    break
            gc.collect()
            stragglers = list(instance.topLevelWidgets())
            if stragglers:
                print(f"[hicksfield tests] WARNING: "
                      f"{len(stragglers)} window(s) still alive at "
                      "shutdown",
                      file=sys.stderr, flush=True)
            instance.quit()
    except Exception:  # noqa: BLE001 - teardown must not raise
        pass
    gc.collect()
    print("[hicksfield tests] qt shutdown done", file=sys.stderr,
          flush=True)


def wait_for(predicate, timeout_ms=5000):
    """Pump the Qt event loop until predicate() is true or the timeout
    passes. (This PySide6 build has no QTest.qWaitFor.)"""
    import time
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        QTest.qWait(20)
