import os
import unittest
from unittest import mock

from hicksfield import voice
from hicksfield.errors import DownloadError, EngineNotReadyError
from tests.helpers import TempEnvTestCase, write_wav


class _ShortResponse:
    """A fake urlopen response serving fewer bytes than expected."""

    status = 200

    def __init__(self, body):
        self._body = body
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        if not self._body:
            return b""
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk


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

    def test_voice_file_sizes_are_the_published_values(self):
        # Verified against the published Piper voice files
        # (rhasspy/piper-voices on HuggingFace) 2026-11-15, and
        # against the downloaded files themselves.
        self.assertEqual(voice.VOICE_FILE_SIZES, {
            "en_US-lessac-medium.onnx": 63201294,
            "en_US-lessac-medium.onnx.json": 4885,
        })

    def test_truncated_voice_file_is_not_ready(self):
        voice_dir = self.tmp / "voice"
        voice_dir.mkdir()
        os.environ["HICKSFIELD_VOICE_DIR"] = str(voice_dir)
        (voice_dir / "en_US-lessac-medium.onnx").write_bytes(b"x" * 10)
        (voice_dir / "en_US-lessac-medium.onnx.json").write_bytes(
            b"{}" + b" " * 4883)
        have, total = voice.voice_status()
        self.assertEqual(total, 2)
        self.assertEqual(have, 1)

    def test_setup_passes_expected_sizes_to_downloader(self):
        voice_dir = self.tmp / "voice"
        os.environ["HICKSFIELD_VOICE_DIR"] = str(voice_dir)
        calls = []

        def _fake_download(url, dest, expected_size=None,
                           progress_cb=None, cancel_event=None):
            calls.append((dest.name, expected_size))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * expected_size)
            return dest

        with mock.patch.object(voice, "download",
                               side_effect=_fake_download), \
                mock.patch.object(voice, "_sha256_file",
                                  return_value=voice.VOICE_MODEL_SHA256):
            voice._setup_voice_files(lambda f, d: None, None)
        self.assertEqual(calls, [
            ("en_US-lessac-medium.onnx", 63201294),
            ("en_US-lessac-medium.onnx.json", 4885),
        ])
        have, total = voice.voice_status()
        self.assertEqual((have, total), (2, 2))

    def test_wrong_size_download_fails_plainly(self):
        voice_dir = self.tmp / "voice"
        os.environ["HICKSFIELD_VOICE_DIR"] = str(voice_dir)
        short = _ShortResponse(b"x" * 10)
        with mock.patch("urllib.request.urlopen", return_value=short):
            with self.assertRaises(DownloadError) as ctx:
                voice._setup_voice_files(lambda f, d: None, None)
        text = ctx.exception.plain_text()
        self.assertIn("10 bytes", text)
        self.assertIn("63201294 bytes", text)

    def test_wrong_checksum_download_fails_plainly(self):
        voice_dir = self.tmp / "voice"
        os.environ["HICKSFIELD_VOICE_DIR"] = str(voice_dir)

        def _fake_download(url, dest, expected_size=None,
                           progress_cb=None, cancel_event=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * expected_size)
            return dest

        with mock.patch.object(voice, "download",
                               side_effect=_fake_download), \
                mock.patch.object(voice, "_sha256_file",
                                  return_value="0" * 64):
            with self.assertRaises(DownloadError) as ctx:
                voice._setup_voice_files(lambda f, d: None, None)
        self.assertIn("checksum", ctx.exception.plain_text())


class VoiceChoiceTests(TempEnvTestCase):
    """Item 11: three real Piper voices, verified sizes."""

    def test_three_real_voices_with_verified_sizes(self):
        # Verified against the published Piper voice files
        # (rhasspy/piper-voices on HuggingFace) 2026-11-15.
        self.assertEqual(set(voice.VOICES), {
            "en_US-lessac-medium", "en_US-ryan-high",
            "en_GB-alan-medium"})
        ryan = voice.VOICES["en_US-ryan-high"]["files"]
        self.assertEqual(ryan["en_US-ryan-high.onnx"]["size"],
                         120786792)
        self.assertEqual(ryan["en_US-ryan-high.onnx"]["sha256"],
                         "b3990d7606e183ec8dbfba70a4607074f162de1a"
                         "0c412e0180d1ff60bb154eca")
        self.assertEqual(ryan["en_US-ryan-high.onnx.json"]["size"],
                         4166)
        self.assertIsNone(
            ryan["en_US-ryan-high.onnx.json"]["sha256"])
        alan = voice.VOICES["en_GB-alan-medium"]["files"]
        self.assertEqual(alan["en_GB-alan-medium.onnx"]["size"],
                         63201294)
        self.assertEqual(alan["en_GB-alan-medium.onnx"]["sha256"],
                         "0a309668932205e762801f1efc2736cd4b012032"
                         "9622adf62be09e56339d3330")
        self.assertEqual(alan["en_GB-alan-medium.onnx.json"]["size"],
                         4888)
        self.assertIsNone(
            alan["en_GB-alan-medium.onnx.json"]["sha256"])
        self.assertTrue(
            voice.VOICES["en_US-lessac-medium"]["recommended"])
        self.assertFalse(
            voice.VOICES["en_US-ryan-high"]["recommended"])
        self.assertFalse(
            voice.VOICES["en_GB-alan-medium"]["recommended"])
        for entry in voice.VOICES.values():
            self.assertTrue(entry["label"])
            self.assertTrue(entry["description"])
            self.assertTrue(entry["base_url"].startswith(
                "https://huggingface.co/"))

    def test_voice_files_per_voice(self):
        files = voice.voice_files("en_GB-alan-medium")
        self.assertEqual([name for name, _url in files],
                         ["en_GB-alan-medium.onnx",
                          "en_GB-alan-medium.onnx.json"])
        self.assertIn("en/en_GB/alan/medium/", files[0][1])
        files = voice.voice_files("en_US-ryan-high")
        self.assertIn("en/en_US/ryan/high/", files[0][1])

    def test_unknown_voice_is_not_ready(self):
        ready, status = voice.is_ready("no-such-voice")
        self.assertFalse(ready)
        self.assertIn("not one HicksField knows", status)
        self.assertEqual(voice.voice_files("no-such-voice"), [])

    def test_setup_passes_ryan_sizes_to_downloader(self):
        voice_dir = self.tmp / "voice"
        os.environ["HICKSFIELD_VOICE_DIR"] = str(voice_dir)
        calls = []

        def _fake_download(url, dest, expected_size=None,
                           progress_cb=None, cancel_event=None):
            calls.append((dest.name, expected_size))
            dest.parent.mkdir(parents=True, exist_ok=True)
            # A file of the right size, without writing 120 MB.
            with open(dest, "wb") as handle:
                handle.truncate(expected_size)
            return dest

        ryan_sha = voice.VOICES["en_US-ryan-high"]["files"][
            "en_US-ryan-high.onnx"]["sha256"]
        with mock.patch.object(voice, "download",
                               side_effect=_fake_download), \
                mock.patch.object(voice, "_sha256_file",
                                  return_value=ryan_sha):
            voice._setup_voice_files(lambda f, d: None, None,
                                     "en_US-ryan-high")
        self.assertEqual(calls, [
            ("en_US-ryan-high.onnx", 120786792),
            ("en_US-ryan-high.onnx.json", 4166),
        ])
        have, total = voice.voice_status("en_US-ryan-high")
        self.assertEqual((have, total), (2, 2))

    def _ready_voice(self, voice_id):
        """A fake piper and right-sized voice files, so the voice
        counts as ready. Returns the voice folder."""
        fake_piper = self.tmp / "piper"
        fake_piper.write_text("x", encoding="utf-8")
        os.environ["HICKSFIELD_PIPER"] = str(fake_piper)
        voice_dir = self.tmp / "voice"
        os.environ["HICKSFIELD_VOICE_DIR"] = str(voice_dir)
        folder = voice.voice_dir(voice_id)
        folder.mkdir(parents=True, exist_ok=True)
        for name, info in voice.VOICES[voice_id]["files"].items():
            with open(folder / name, "wb") as handle:
                handle.truncate(info["size"])
        return folder

    def test_synthesize_uses_the_chosen_voice(self):
        folder = self._ready_voice("en_US-ryan-high")
        ryan_sha = voice.VOICES["en_US-ryan-high"]["files"][
            "en_US-ryan-high.onnx"]["sha256"]
        proc = mock.Mock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        with mock.patch.object(voice, "_sha256_file",
                               return_value=ryan_sha), \
                mock.patch("subprocess.Popen",
                           return_value=proc) as popen, \
                mock.patch.object(voice, "wav_duration",
                                  return_value=1.0):
            _path, duration = voice.synthesize(
                "hello", self.tmp / "out.wav",
                voice_id="en_US-ryan-high")
        self.assertEqual(duration, 1.0)
        args = popen.call_args[0][0]
        self.assertIn(str(folder / "en_US-ryan-high.onnx"), args)

    def test_synthesize_defaults_to_lessac(self):
        folder = self._ready_voice("en_US-lessac-medium")
        proc = mock.Mock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        with mock.patch.object(voice, "_sha256_file",
                               return_value=voice.VOICE_MODEL_SHA256), \
                mock.patch("subprocess.Popen",
                           return_value=proc) as popen, \
                mock.patch.object(voice, "wav_duration",
                                  return_value=1.0):
            voice.synthesize("hello", self.tmp / "out.wav")
        args = popen.call_args[0][0]
        self.assertIn(str(folder / "en_US-lessac-medium.onnx"), args)


if __name__ == "__main__":
    unittest.main()
