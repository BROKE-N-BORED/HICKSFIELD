import logging
import unittest

from hicksfield import debuglog
from tests.helpers import TempEnvTestCase


class DebugLogTests(TempEnvTestCase):
    def setUp(self):
        super().setUp()
        self._reset_logger()
        debuglog.clear_ring()

    def tearDown(self):
        self._reset_logger()
        super().tearDown()

    @staticmethod
    def _reset_logger():
        # The logger is global; its file handler points at whichever
        # temp dir was set up first, and that dir is deleted by the
        # owning test's tearDown. Drop all handlers so each test gets
        # a fresh handler in its own temp dir.
        logger = logging.getLogger(debuglog.LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def test_scrub_replaces_home(self):
        text = debuglog.scrub_text("/home/joshua/film/x", home="/home/joshua")
        self.assertEqual(text, "~/film/x")

    def test_scrub_redacts_secrets(self):
        for key in ("api_key", "token", "password", "secret"):
            text = debuglog.scrub_text(f"login {key}=abc123")
            self.assertNotIn("abc123", text, key)
            self.assertIn("<redacted>", text, key)

    def test_ring_keeps_last_200(self):
        logger = debuglog.setup_debug_log(self.tmp / "logs")
        for index in range(250):
            logger.info("line %d", index)
        records = debuglog.ring_records()
        self.assertEqual(len(records), 200)
        self.assertIn("line 249", records[-1])
        self.assertNotIn("line 0", "\n".join(records))

    def test_file_log_created(self):
        logger = debuglog.setup_debug_log(self.tmp / "logs")
        logger.info("hello log")
        log_file = self.tmp / "logs" / "hicksfield.log"
        self.assertTrue(log_file.exists())
        self.assertIn("hello log", log_file.read_text(encoding="utf-8"))

    def test_debug_report_scrubs_and_names_version(self):
        logger = debuglog.setup_debug_log(self.tmp / "logs")
        logger.info("api_key=should-not-appear")
        report = debuglog.build_debug_report(
            "0.2.0", "RAM: 8000 MB total.")
        self.assertIn("HicksField 0.2.0", report)
        self.assertIn("RAM: 8000 MB total.", report)
        self.assertNotIn("should-not-appear", report)
        self.assertIn("<redacted>", report)


if __name__ == "__main__":
    unittest.main()
