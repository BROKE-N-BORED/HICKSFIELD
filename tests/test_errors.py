import unittest

from hicksfield.errors import (
    CancelledError,
    DownloadError,
    FFmpegMissingError,
    HicksFieldError,
    OllamaUnreachableError,
)


class ErrorTests(unittest.TestCase):
    def test_plain_text_with_hint(self):
        err = HicksFieldError("Something broke.", "Try again.")
        self.assertEqual(err.plain_text(), "Something broke.\nTry again.")

    def test_plain_text_without_hint(self):
        err = HicksFieldError("Something broke.")
        self.assertEqual(err.plain_text(), "Something broke.")

    def test_ffmpeg_error_names_the_fix(self):
        err = FFmpegMissingError()
        self.assertIn("installer", err.plain_text().lower())
        self.assertNotIn("sudo apt", err.plain_text())
        self.assertIn("ffmpeg", err.message.lower())

    def test_ollama_error_offers_manual_scenes(self):
        err = OllamaUnreachableError()
        self.assertIn("Ollama", err.message)
        self.assertIn("type your scenes", err.plain_text())

    def test_cancelled_is_plain(self):
        self.assertEqual(CancelledError().plain_text(), "Cancelled.")

    def test_download_error_is_hicksfield_error(self):
        self.assertIsInstance(DownloadError("x"), HicksFieldError)

    def test_no_em_dash_in_builtin_messages(self):
        for err in (FFmpegMissingError(), OllamaUnreachableError(),
                    CancelledError()):
            self.assertNotIn("\u2014", err.plain_text())
            self.assertNotIn("&", err.plain_text())


if __name__ == "__main__":
    unittest.main()
