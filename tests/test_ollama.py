import json
import os
import unittest
from unittest import mock

from hicksfield import ollama
from hicksfield.errors import HicksFieldError, OllamaUnreachableError
from tests.helpers import TempEnvTestCase

GOOD = json.dumps([
    {"prompt": "a stool in a workshop", "narration": "A stool sits."},
    {"prompt": "a hammer on the bench", "narration": "A hammer waits."},
])


class ParseScenesTests(unittest.TestCase):
    def test_plain_json(self):
        scenes, dropped, _raw = ollama.parse_scenes_reply(GOOD)
        self.assertEqual(len(scenes), 2)
        self.assertEqual(dropped, 0)
        self.assertEqual(scenes[0]["prompt"], "a stool in a workshop")

    def test_fenced_and_prose(self):
        reply = (
            "Here are your scenes:\n```json\n" + GOOD + "\n```\nEnjoy!"
        )
        scenes, dropped, _raw = ollama.parse_scenes_reply(reply)
        self.assertEqual(len(scenes), 2)

    def test_incomplete_entries_dropped_and_counted(self):
        reply = json.dumps([
            {"prompt": "a stool", "narration": "A stool sits."},
            {"prompt": "no line"},
            {"narration": "no picture"},
            "just a string",
            {"prompt": "  ", "narration": "blank prompt"},
        ])
        scenes, dropped, _raw = ollama.parse_scenes_reply(reply)
        self.assertEqual(len(scenes), 1)
        self.assertEqual(dropped, 4)

    def test_no_list_raises(self):
        with self.assertRaises(HicksFieldError):
            ollama.parse_scenes_reply("no json here at all")

    def test_unreadable_json_raises(self):
        with self.assertRaises(HicksFieldError):
            ollama.parse_scenes_reply('[{"prompt": broken')

    def test_empty_usable_list_raises(self):
        with self.assertRaises(HicksFieldError):
            ollama.parse_scenes_reply('[{"prompt": "only"}]')


class OllamaClientTests(TempEnvTestCase):
    def test_list_models(self):
        payload = {"models": [{"name": "a:2b"}, {"name": "b:2b"},
                              {"nope": True}]}
        with mock.patch.object(ollama, "_request_json",
                               return_value=payload) as req:
            self.assertEqual(ollama.list_models(), ["a:2b", "b:2b"])
            req.assert_called_once_with("GET", "/api/tags", timeout=10)

    def test_break_into_scenes(self):
        payload = {"message": {"content": GOOD}}
        with mock.patch.object(ollama, "_request_json",
                               return_value=payload):
            scenes, dropped, raw = ollama.break_into_scenes(
                "a stool film", "a:2b")
        self.assertEqual(len(scenes), 2)
        self.assertEqual(dropped, 0)
        self.assertEqual(raw, GOOD)

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


if __name__ == "__main__":
    unittest.main()
