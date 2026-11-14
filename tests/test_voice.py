import os
import unittest

from hicksfield import voice
from hicksfield.errors import EngineNotReadyError
from tests.helpers import TempEnvTestCase, write_wav


class VoiceTests(TempEnvTestCase):
    def test_voice_files(self):
        self.assertEqual(voice.VOICE_ID, "en_US-lessac-medium")
        self.assertEqual(len(voice.VOICE_FILES), 2)
        self.assertTrue(voice.VOICE_FILES[0].endswith(".onnx"))
        self.assertTrue(voice.VOICE_FILES[1].endswith(".onnx.json"))
        for _name, url in voice.voice_files():
            self.assertTrue(url.startswith("https://huggingface.co/"))

    def test_voice_dir_override(self):
        os.environ["HICKSFIELD_VOICE_DIR"] = str(self.tmp / "voice")
        self.assertEqual(voice.voice_dir(), self.tmp / "voice")

    def test_find_piper_override(self):
        fake = self.tmp / "piper"
        fake.write_text("x", encoding="utf-8")
        os.environ["HICKSFIELD_PIPER"] = str(fake)
        self.assertEqual(voice.find_piper(), fake)

    def test_not_ready_without_piper(self):
        if voice.find_piper() is not None:
            self.skipTest("piper is installed in this environment")
        os.environ["HICKSFIELD_PIPER"] = str(self.tmp / "no-piper-here")
        ready, status = voice.is_ready()
        self.assertFalse(ready)
        self.assertIn("missing", status)

    def test_synthesize_raises_when_not_ready(self):
        if voice.find_piper() is not None:
            self.skipTest("piper is installed in this environment")
        os.environ["HICKSFIELD_PIPER"] = str(self.tmp / "no-piper-here")
        with self.assertRaises(EngineNotReadyError):
            voice.synthesize("hello", self.tmp / "out.wav")

    def test_wav_duration(self):
        path = write_wav(self.tmp / "one.wav", seconds=1.0)
        duration = voice.wav_duration(path)
        self.assertAlmostEqual(duration, 1.0, places=2)

    def test_wav_duration_unreadable(self):
        self.assertIsNone(voice.wav_duration(self.tmp / "missing.wav"))
        bad = self.tmp / "bad.wav"
        bad.write_text("not a wav", encoding="utf-8")
        self.assertIsNone(voice.wav_duration(bad))


if __name__ == "__main__":
    unittest.main()
