import json
import os
import unittest
from unittest import mock

from hicksfield import ollama
from hicksfield.errors import HicksFieldError, OllamaUnreachableError
from tests.helpers import TempEnvTestCase


class OllamaClientTests(TempEnvTestCase):
    def test_list_models(self):
        payload = {"models": [{"name": "a:2b"}, {"name": "b:2b"},
                              {"nope": True}]}
        with mock.patch.object(ollama, "_request_json",
                               return_value=payload) as req:
            self.assertEqual(ollama.list_models(), ["a:2b", "b:2b"])
            req.assert_called_once_with("GET", "/api/tags", timeout=10)

    def test_unreachable_server(self):
        os.environ["HICKSFIELD_OLLAMA_URL"] = "http://127.0.0.1:1"
        self.assertFalse(ollama.is_reachable(timeout=2))
        with self.assertRaises(OllamaUnreachableError):
            ollama.list_models(timeout=2)

    def test_http_error_is_plain(self):
        import urllib.error
        error = urllib.error.HTTPError(
            "http://x", 500, "boom", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HicksFieldError) as ctx:
                ollama.list_models(timeout=2)
            self.assertIn("500", str(ctx.exception))


class SystemPromptTests(unittest.TestCase):
    """Item 12: the breakdown prompt asks for the picked length."""

    def test_medium_is_the_usual_prompt(self):
        self.assertEqual(ollama.system_prompt("medium"),
                         ollama.SYSTEM_PROMPT)
        self.assertIn("Make 3 to 8 scenes", ollama.SYSTEM_PROMPT)

    def test_short_and_long_ask_for_their_counts(self):
        self.assertIn("Make 3 to 5 scenes",
                      ollama.system_prompt("short"))
        self.assertIn("Make 8 to 12 scenes",
                      ollama.system_prompt("long"))

    def test_only_the_count_changes(self):
        short = ollama.system_prompt("short")
        long_ = ollama.system_prompt("long")
        self.assertEqual(
            short.replace("3 to 5", "N to M"),
            long_.replace("8 to 12", "N to M"))

    def test_unknown_length_falls_back_to_medium(self):
        self.assertEqual(ollama.system_prompt("huge"),
                         ollama.system_prompt("medium"))

    def test_streaming_sends_the_chosen_prompt(self):
        captured = {}

        class _Stream:
            def __init__(self):
                self.lines = [
                    (json.dumps(
                        {"message": {"content":
                                     '[{"prompt": "a stool", '
                                     '"narration": "A stool."}]'},
                         "done": False}) + "\n").encode("utf-8"),
                    (json.dumps({"done": True}) + "\n").encode("utf-8"),
                ]

            def readline(self):
                return self.lines.pop(0) if self.lines else b""

            def close(self):
                pass

        def _capture(request, timeout=None):
            captured["payload"] = json.loads(
                request.data.decode("utf-8"))
            return _Stream()

        with mock.patch.object(ollama.urllib.request, "urlopen",
                               side_effect=_capture):
            scenes, _dropped, _raw = (
                ollama.break_into_scenes_streaming(
                    "an idea", "fake-model", lambda s: None,
                    scene_length="long"))
        self.assertEqual(len(scenes), 1)
        messages = captured["payload"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Make 8 to 12 scenes", messages[0]["content"])

    def test_streaming_defaults_to_the_usual_prompt(self):
        captured = {}

        class _Stream:
            def __init__(self):
                self.lines = [
                    (json.dumps(
                        {"message": {"content":
                                     '[{"prompt": "a stool", '
                                     '"narration": "A stool."}]'},
                         "done": False}) + "\n").encode("utf-8"),
                    (json.dumps({"done": True}) + "\n").encode("utf-8"),
                ]

            def readline(self):
                return self.lines.pop(0) if self.lines else b""

            def close(self):
                pass

        def _capture(request, timeout=None):
            captured["payload"] = json.loads(
                request.data.decode("utf-8"))
            return _Stream()

        with mock.patch.object(ollama.urllib.request, "urlopen",
                               side_effect=_capture):
            ollama.break_into_scenes_streaming(
                "an idea", "fake-model", lambda s: None)
        messages = captured["payload"]["messages"]
        self.assertEqual(messages[0]["content"], ollama.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
