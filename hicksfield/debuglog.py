"""Debug log layer (BNB S61 + T21).

- Rotating timestamped log files, 1 MB each, 3 backups.
- Last-200 ring buffer kept in memory for the debug report.
- Uncaught exceptions land in the log with a full traceback.
- The debug report is scrubbed: home paths become ~, and anything shaped
  like a key, token, or password is redacted. HicksField keeps no secrets,
  but the scrubber runs anyway.
"""

import logging
import logging.handlers
import re
import sys
import traceback
from collections import deque
from pathlib import Path

LOGGER_NAME = "hicksfield"
RING_SIZE = 200
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_ring = deque(maxlen=RING_SIZE)
_hook_installed = False


class RingBufferHandler(logging.Handler):
    """Keeps the last RING_SIZE formatted records in memory."""

    def emit(self, record):
        try:
            _ring.append(self.format(record))
        except Exception:
            self.handleError(record)


_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|bearer)\s*[:=]\s*\S+"
)


def scrub_text(text, home=None):
    """Remove home paths and secret-shaped values from report text."""
    if home is None:
        home = str(Path.home())
    out = text.replace(home, "~")
    out = _SECRET_RE.sub(r"\1=<redacted>", out)
    return out


def setup_debug_log(log_dir):
    """Set up file + ring logging. Returns the hicksfield logger."""
    global _hook_installed
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not any(isinstance(h, logging.handlers.RotatingFileHandler)
               for h in logger.handlers):
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "hicksfield.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(file_handler)
    if not any(isinstance(h, RingBufferHandler) for h in logger.handlers):
        ring_handler = RingBufferHandler()
        ring_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(ring_handler)
    if not _hook_installed:
        _hook_installed = True
        _install_excepthook(logger)
    return logger


def _install_excepthook(logger):
    previous = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        logger.error(
            "Uncaught exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def ring_records():
    return list(_ring)


def clear_ring():
    _ring.clear()


def build_debug_report(version, machine_summary):
    """Build the scrubbed debug report shown by the Debug report button."""
    lines = [
        f"HicksField {version} debug report",
        "",
        "Machine:",
        machine_summary or "(not measured)",
        "",
        "Recent log (newest last):",
    ]
    lines.extend(ring_records() or ["(log is empty)"])
    return scrub_text("\n".join(lines))
