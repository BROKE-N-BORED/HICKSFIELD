import http.server
import socketserver
import threading
import unittest
from functools import partial

from hicksfield import downloader
from hicksfield.downloader import format_bytes
from hicksfield.errors import CancelledError, DownloadError
from tests.helpers import TempEnvTestCase

PAYLOAD = bytes(range(256)) * 40  # 10240 bytes


class RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD, honouring Range, and records requests."""

    requests_seen = []

    def log_message(self, _format, *_args):
        pass

    def _serve(self):
        RangeHandler.requests_seen.append(self.headers.get("Range"))
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header[len("bytes="):].split("-")[0])
            if start >= len(PAYLOAD):
                self.send_response(416)
                self.end_headers()
                return
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(PAYLOAD) - 1}/"
                             f"{len(PAYLOAD)}")
        else:
            body = PAYLOAD
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        self._serve()


class NoRangeHandler(RangeHandler):
    """Ignores Range headers entirely (returns 200 + full body)."""

    def _serve(self):
        RangeHandler.requests_seen.append(self.headers.get("Range"))
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


class ServerTestCase(TempEnvTestCase):
    handler = RangeHandler

    def setUp(self):
        super().setUp()
        RangeHandler.requests_seen = []
        self.server = socketserver.TCPServer(
            ("127.0.0.1", 0), self.handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def url(self, path="/file.bin"):
        return f"http://127.0.0.1:{self.port}{path}"


class DownloaderTests(ServerTestCase):
    def test_full_download(self):
        dest = self.tmp / "file.bin"
        progress = []
        out = downloader.download(
            self.url(), dest, expected_size=len(PAYLOAD),
            progress_cb=lambda d, t: progress.append((d, t)))
        self.assertEqual(out, dest)
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (len(PAYLOAD), len(PAYLOAD)))

    def test_resume_after_partial(self):
        dest = self.tmp / "file.bin"
        part = dest.with_suffix(".bin.part")
        part.write_bytes(PAYLOAD[:4000])
        downloader.download(self.url(), dest, expected_size=len(PAYLOAD))
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertIn("bytes=4000-", RangeHandler.requests_seen)
        self.assertFalse(part.exists())

    def test_server_ignoring_range_restarts(self):
        dest = self.tmp / "file.bin"
        part = dest.with_suffix(".bin.part")
        part.write_bytes(b"junk junk junk")
        # Swap in the no-range server for this one test.
        self.server.shutdown()
        self.server.server_close()
        self.server = socketserver.TCPServer(
            ("127.0.0.1", 0), NoRangeHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        downloader.download(self.url(), dest, expected_size=len(PAYLOAD))
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_size_mismatch_raises(self):
        dest = self.tmp / "file.bin"
        with self.assertRaises(DownloadError):
            downloader.download(self.url(), dest,
                                expected_size=len(PAYLOAD) + 1)

    def test_already_downloaded_skips_request(self):
        dest = self.tmp / "file.bin"
        dest.write_bytes(PAYLOAD)
        downloader.download(self.url(), dest, expected_size=len(PAYLOAD))
        self.assertEqual(RangeHandler.requests_seen, [])

    def test_404_raises_download_error(self):
        with self.assertRaises(DownloadError):
            downloader.download(self.url("/missing"), self.tmp / "x.bin")

    def test_cancel_before_start(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            downloader.download(self.url(), self.tmp / "x.bin",
                                cancel_event=event)

    def test_format_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(2 * 1024 * 1024), "2.0 MB")
        self.assertEqual(format_bytes(int(2.1 * 1024 ** 3)), "2.1 GB")


if __name__ == "__main__":
    unittest.main()
