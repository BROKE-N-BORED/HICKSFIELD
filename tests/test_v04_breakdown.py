"""v0.4.0 GROUP A tests: task list 3 items 1, 2, 16, 31, 32, 33, 34, 38.

Item 1: a quiet stretch past 10 s must not kill the breakdown with a
raw socket error -- a timeout is a retryable event on a live socket,
and the heartbeat and silence limit stay reachable. (The live proof
is the fake-server probe in probe_item1.py, outside the shipped tree;
these tests cover the same paths with stand-in streams.)
Item 2: the wait for the first headers gets the same silence budget
as the rest of the stream, and reports "loading" while the model is
still loading.
Item 16: mid-stream connection errors fail plainly, never raw.
Item 31: the model check asks models in parallel with progress, and
Refresh is disabled while a check runs.
Item 32: the recommendation is the best model that fits, not the
smallest.
Item 33: fit is judged on available memory, not total memory.
Item 34: breakdown arrivals append a row instead of rebuilding the
whole list.
Item 38: Break stays disabled until a model is picked.
"""

import json
import socket
import threading
import time
import unittest
import urllib.error
from unittest import mock

from PySide6.QtWidgets import QLabel

from hicksfield import machine, ollama, scenes as scenes_mod
from hicksfield.errors import HicksFieldError, OllamaUnreachableError
from hicksfield.gui import ScenesWindow
from tests.helpers import TempEnvTestCase, wait_for
from tests.test_gui import GuiBase

GB = 1024 * 1024 * 1024


def _chunk(content, done=False):
    return (json.dumps(
        {"message": {"content": content}, "done": done})
        + "\n").encode("utf-8")


class _FakeStream:
    """A stand-in for an Ollama streaming response."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def close(self):
        self.closed = True


class _TimeoutThenDeliverStream:
    """Times out twice, then delivers: a timeout is retryable."""

    def __init__(self, lines, timeouts=2):
        self._lines = list(lines)
        self._timeouts = timeouts
        self.closed = False

    def readline(self):
        if self._timeouts > 0:
            self._timeouts -= 1
            raise socket.timeout("timed out")
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def close(self):
        self.closed = True


class _ResetStream:
    """Answers once, then the connection is reset."""

    def __init__(self):
        self._sent = False
        self.closed = False

    def readline(self):
        if not self._sent:
            self._sent = True
            return _chunk(
                '[{"prompt": "a stool", "narration": "A stool."}]')
        raise ConnectionResetError("connection reset by peer")

    def close(self):
        self.closed = True


class _PoisonedStream:
    """The v0.3 failure shape: a timeout, then the poisoned-stream
    OSError on the very next read."""

    def __init__(self):
        self._timed_out = False
        self.closed = False

    def readline(self):
        if not self._timed_out:
            self._timed_out = True
            raise socket.timeout("timed out")
        raise OSError("cannot read from timed out object")

    def close(self):
        self.closed = True


def _one_scene_lines():
    return [
        _chunk('[{"prompt": "a stool", "narration": "A stool."}]'),
        _chunk("", done=True),
    ]


class StreamingTimeoutTests(TempEnvTestCase):
    """Item 1: a timeout is a retryable event, never a raw error."""

    def test_timeout_then_delivery_completes(self):
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_TimeoutThenDeliverStream(
                    _one_scene_lines())):
            scenes, _dropped, _raw = (
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None))
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["prompt"], "a stool")

    def test_poisoned_stream_error_never_shown_raw(self):
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_PoisonedStream()):
            with self.assertRaises(HicksFieldError) as caught:
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None)
        self.assertNotIn("timed out object", str(caught.exception))
        self.assertNotIn("OSError", str(caught.exception))

    def test_heartbeat_still_reports_while_waiting(self):
        reports = []
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_TimeoutThenDeliverStream(
                    _one_scene_lines(), timeouts=10)):
            # Ten instant timeouts, then the scene: the loop reports
            # a heartbeat while it waits instead of dying.
            scenes, _dropped, _raw = (
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None,
                    report=lambda label, _frac, detail: reports.append(
                        (label, detail)),
                    heartbeat_s=0.05))
        self.assertEqual(len(scenes), 1)
        self.assertTrue(
            any("waiting" in detail for _label, detail in reports),
            reports)


class StreamingConnectionTests(TempEnvTestCase):
    """Item 16: mid-stream trouble fails plainly."""

    def test_reset_fails_plainly(self):
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                return_value=_ResetStream()):
            with self.assertRaises(HicksFieldError) as caught:
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None)
        self.assertIn("dropped", str(caught.exception))
        self.assertIn("try again", caught.exception.plain_text())
        self.assertNotIn("ConnectionResetError",
                         str(caught.exception))

    def test_cancel_still_answers_quickly(self):
        cancel = threading.Event()
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            from hicksfield.errors import CancelledError
            started = time.monotonic()
            with mock.patch.object(
                    ollama.urllib.request, "urlopen",
                    return_value=_TimeoutThenDeliverStream(
                        _one_scene_lines(), timeouts=10 ** 6)):
                with self.assertRaises(CancelledError):
                    ollama.break_into_scenes_streaming(
                        "an idea", "fake-model", lambda s: None,
                        cancel_event=cancel)
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            timer.cancel()


class OpenPhaseTests(TempEnvTestCase):
    """Item 2: the first headers get the silence budget too."""

    def test_urlopen_gets_the_silence_budget(self):
        captured = {}

        def _capture(request, timeout=None):
            captured["timeout"] = timeout
            return _FakeStream(_one_scene_lines())

        with mock.patch.object(ollama.urllib.request, "urlopen",
                               side_effect=_capture):
            ollama.break_into_scenes_streaming(
                "an idea", "fake-model", lambda s: None,
                silence_limit_s=42)
        self.assertEqual(captured["timeout"], 42)

    def test_slow_headers_report_loading_then_complete(self):
        reports = []

        def _slow_open(request, timeout=None):
            time.sleep(0.3)
            return _FakeStream(_one_scene_lines())

        with mock.patch.object(ollama.urllib.request, "urlopen",
                               side_effect=_slow_open):
            scenes, _dropped, _raw = (
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None,
                    report=lambda label, _frac, detail: reports.append(
                        (label, detail)),
                    silence_limit_s=5, heartbeat_s=0.1))
        self.assertEqual(len(scenes), 1)
        loadings = [d for label, d in reports if label == "loading"]
        self.assertTrue(loadings, reports)

    def test_headers_never_arrive_says_still_loading(self):
        def _never_open(request, timeout=None):
            time.sleep(5)
            return _FakeStream(_one_scene_lines())

        with mock.patch.object(ollama.urllib.request, "urlopen",
                               side_effect=_never_open):
            with self.assertRaises(HicksFieldError) as caught:
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None,
                    silence_limit_s=0.3, heartbeat_s=5)
        self.assertIn("still loading", str(caught.exception))
        self.assertNotIsInstance(caught.exception,
                                 OllamaUnreachableError)

    def test_urlopen_timeout_means_still_loading(self):
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                side_effect=socket.timeout("timed out")):
            with self.assertRaises(HicksFieldError) as caught:
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None)
        self.assertIn("still loading", str(caught.exception))
        self.assertNotIsInstance(caught.exception,
                                 OllamaUnreachableError)

    def test_refused_connection_still_means_not_running(self):
        with mock.patch.object(
                ollama.urllib.request, "urlopen",
                side_effect=urllib.error.URLError("nope")):
            with self.assertRaises(OllamaUnreachableError):
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None)

    def test_http_error_still_plain(self):
        error = urllib.error.HTTPError(
            "http://x", 500, "boom", {}, None)
        with mock.patch.object(ollama.urllib.request, "urlopen",
                               side_effect=error):
            with self.assertRaises(HicksFieldError) as caught:
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None)
        self.assertIn("500", str(caught.exception))


class ModelFitsAvailableTests(unittest.TestCase):
    """Item 33: fit is judged on available memory."""

    def test_available_memory_rules(self):
        # 8 GB total, only 2 GB free: a 1.5 GB model does not fit,
        # even though it would against the total.
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=2048), \
                mock.patch.object(machine, "mem_total_mb",
                                  return_value=8192):
            self.assertFalse(ollama.model_fits(1500000000))
            self.assertTrue(ollama.model_fits(500000000))

    def test_plenty_free_fits(self):
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=8192):
            self.assertTrue(ollama.model_fits(1500000000))
            self.assertFalse(ollama.model_fits(5000000000))

    def test_unknown_available_never_blocks(self):
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=0):
            self.assertTrue(ollama.model_fits(5000000000))


class RecommendedModelBestTests(unittest.TestCase):
    """Item 32: the best model that fits, not the smallest."""

    TINY = {"name": "tiny:0.5b", "size": 500000000,
            "parameter_size": "0.5B", "capabilities": ["chat"]}
    MID = {"name": "mid:2b", "size": 1500000000,
           "parameter_size": "2.0B", "capabilities": ["chat"]}
    BIG = {"name": "big:7b", "size": 7000000000,
           "parameter_size": "7.0B", "capabilities": ["chat"]}
    QUIET = {"name": "quiet:1b", "size": 800000000,
             "parameter_size": "1.0B", "capabilities": ["completion"]}

    def test_bigger_fitting_model_wins(self):
        best = ollama.recommended_model(
            [self.TINY, self.MID], 8192)
        self.assertEqual(best["name"], "mid:2b")

    def test_too_big_still_excluded(self):
        best = ollama.recommended_model(
            [self.BIG, self.MID, self.TINY], 8192)
        self.assertEqual(best["name"], "mid:2b")

    def test_completion_only_still_excluded(self):
        self.assertIsNone(ollama.recommended_model(
            [self.QUIET], 8192))

    def test_more_parameters_beat_more_bytes(self):
        heavy_small = {"name": "small-heavy:2b",
                       "size": 2000000000,
                       "parameter_size": "2.0B",
                       "capabilities": ["chat"]}
        light_big = {"name": "big-light:7b",
                     "size": 1200000000,
                     "parameter_size": "7.0B",
                     "capabilities": ["chat"]}
        best = ollama.recommended_model(
            [heavy_small, light_big], 8192)
        self.assertEqual(best["name"], "big-light:7b")

    def test_unknown_shape_loses_to_known(self):
        mystery = {"name": "mystery", "size": 1500000000,
                   "capabilities": ["chat"]}
        best = ollama.recommended_model([mystery, self.MID], 8192)
        self.assertEqual(best["name"], "mid:2b")


class ModelCheckParallelTests(unittest.TestCase):
    """Item 31: the model check runs in parallel, with progress."""

    def test_capabilities_checked_in_parallel_with_progress(self):
        infos = [{"name": "a:1b"}, {"name": "b:2b"}, {"name": "c:3b"}]
        reports = []

        def _slow_caps(name):
            time.sleep(0.3)
            return ["chat"]

        with mock.patch.object(ollama, "list_model_info",
                               return_value=[dict(i) for i in infos]), \
                mock.patch.object(ollama, "model_capabilities",
                                  side_effect=_slow_caps):
            started = time.monotonic()
            result = ScenesWindow._check_ollama(
                lambda label, frac, detail: reports.append(
                    (label, frac, detail)),
                None)
            elapsed = time.monotonic() - started
        # Sequential would take 0.9 s; parallel takes ~0.3 s.
        self.assertLess(elapsed, 0.8)
        self.assertEqual(len(result), 3)
        self.assertTrue(all(info["capabilities"] == ["chat"]
                            for info in result))
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports[0][0], "models")
        self.assertIn("3 of 3", reports[-1][2])

    def test_empty_model_list_reports_nothing(self):
        reports = []
        with mock.patch.object(ollama, "list_model_info",
                               return_value=[]):
            result = ScenesWindow._check_ollama(
                lambda label, frac, detail: reports.append(detail),
                None)
        self.assertEqual(result, [])
        self.assertEqual(reports, [])


INFOS = [
    {"name": "tiny:0.5b", "size": 500 * 1024 * 1024,
     "parameter_size": "0.5B", "capabilities": ["chat"]},
    {"name": "mid:2b", "size": int(1.5 * GB),
     "parameter_size": "2.0B", "capabilities": ["chat"]},
    {"name": "big:7b", "size": 7 * GB,
     "parameter_size": "7.0B", "capabilities": ["chat"]},
]


class BreakdownGuiTests(GuiBase):
    """Items 31, 32, 33, 34, 38 on the Scenes window."""

    def setUp(self):
        super().setUp()
        # Pin the machine to 8 GB free so "fits this PC" is about
        # the test data, not whatever machine runs the tests.
        self._mem_patch = mock.patch.object(
            machine, "mem_available_mb", return_value=8192)
        self._mem_patch.start()
        self.addCleanup(self._mem_patch.stop)

    def _settled_window(self):
        """A window whose first Ollama check has landed (failed,
        against the dead test port)."""
        window = self._show(ScenesWindow)
        self.assertTrue(wait_for(
            lambda: "not running" in window.ollama_label.text(),
            5000))
        return window

    def test_break_disabled_until_model_picked(self):
        window = self._settled_window()
        self.assertFalse(window.break_button.isEnabled())
        window._on_models([dict(info) for info in INFOS])
        # Models listed, none picked: still disabled.
        self.assertFalse(window.break_button.isEnabled())
        window.model_combo.setCurrentIndex(1)
        self.assertTrue(window.break_button.isEnabled())
        window.model_combo.setCurrentIndex(-1)
        self.assertFalse(window.break_button.isEnabled())

    def test_refresh_disabled_during_check(self):
        window = self._settled_window()

        def _slow_list():
            time.sleep(0.6)
            return []

        with mock.patch.object(ollama, "list_model_info",
                               side_effect=_slow_list):
            window._refresh_models()
            self.assertFalse(window.refresh_button.isEnabled())
            self.assertFalse(window.break_button.isEnabled())
            # A second press while the check runs starts nothing.
            worker = window._worker
            window._refresh_models()
            self.assertIs(window._worker, worker)
            self.assertTrue(wait_for(
                lambda: window.refresh_button.isEnabled(), 5000))
        self.assertFalse(window._checking_models)

    def test_models_progress_shows_the_check(self):
        window = self._settled_window()
        window._on_models_progress(
            "models", 0.5, "1 of 2 models checked")
        self.assertIn("Checking models. 1 of 2 models checked",
                      window.status_label.text())

    def test_best_fitting_model_recommended(self):
        window = self._settled_window()
        window._on_models([dict(info) for info in INFOS])
        # big:7b does not fit 8 GB free; of the two that do, the
        # 2B model is the recommendation, not the 0.5B one.
        self.assertIn("Recommended", window.model_note.text())
        self.assertIn("mid:2b", window.model_note.text())

    def test_model_note_uses_free_memory(self):
        window = self._settled_window()
        with mock.patch.object(machine, "mem_available_mb",
                               return_value=2048):
            window._on_models([dict(info) for info in INFOS])
            window.model_combo.setCurrentIndex(1)
            self.assertIn("too big for this PC",
                          window.model_note.text())
            self.assertIn("free", window.model_note.text())

    def test_show_incoming_appends_instead_of_rebuilding(self):
        window = self._settled_window()
        window._breakdown_running = True
        window._incoming = [scenes_mod.Scene(
            prompt="a stool", narration="A stool.")]
        window._show_incoming()
        self.assertEqual(window.scene_list.count(), 4)
        first_widget = window.scene_list.itemWidget(
            window.scene_list.item(0))
        window._incoming.append(scenes_mod.Scene(
            prompt="a chair", narration="A chair."))
        window._show_incoming()
        self.assertEqual(window.scene_list.count(), 5)
        # The first row is the same widget: it was not rebuilt.
        self.assertIs(window.scene_list.itemWidget(
            window.scene_list.item(0)), first_widget)
        # The new scene sits ahead of the waiting rows.
        second = window.scene_list.itemWidget(
            window.scene_list.item(1))
        labels = second.findChildren(QLabel)
        self.assertTrue(any("Scene 2" in label.text()
                            for label in labels),
                        [label.text() for label in labels])
        window._breakdown_running = False

    def test_loading_progress_says_still_loading(self):
        window = self._settled_window()
        window._on_breakdown_progress("loading", None, "(15s in)")
        self.assertIn("Ollama is still loading the model",
                      window.status_label.text())
        self.assertIn("(15s in)", window.status_label.text())


if __name__ == "__main__":
    unittest.main()
