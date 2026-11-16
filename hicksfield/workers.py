"""Workers: long jobs run off the GUI thread.

One heavy job at a time across the whole app (U25): the JobLock is
checked before a job starts, and a second job is refused with a plain
message naming the job already running.

The work itself runs on a plain Python thread (daemon), and results
come back through Qt signals, which Qt queues to the GUI thread. A
plain thread is used on purpose: a QThread owned by a window that
closes mid-job aborts the whole process when it is destroyed while
still running. With a plain thread, closing the window mid-job is
safe: the signals check that the worker still exists before emitting,
and the lock release runs through a plain callback that does not need
the window.
"""

import logging
import threading
import weakref

from PySide6.QtCore import QObject, Signal
from shiboken6 import isValid

logger = logging.getLogger("hicksfield.workers")

# Workers still running, for a clean shutdown: the test suite (and any
# embedder) can cancel and join them before Qt goes away, so no thread
# is left emitting signals while the interpreter tears down.
_LIVE_WORKERS = weakref.WeakSet()
_LIVE_LOCK = threading.Lock()


class JobLock:
    """One heavy job at a time. Plain words when a second job is refused."""

    def __init__(self):
        self._owner = None
        self._lock = threading.Lock()

    def try_start(self, owner):
        with self._lock:
            if self._owner is not None:
                logger.info("Job refused: %s is already running",
                            self._owner)
                return False
            self._owner = owner
            logger.info("Job lock taken by %s", owner)
            return True

    def finish(self):
        with self._lock:
            if self._owner is not None:
                logger.info("Job lock released by %s", self._owner)
            self._owner = None

    @property
    def owner(self):
        with self._lock:
            return self._owner

    @property
    def busy(self):
        return self.owner is not None


class Worker(QObject):
    """Run a function in a thread, forwarding progress to the GUI.

    The function gets (report, cancel_event). report(label, fraction,
    detail) is called from the worker thread and re-emitted as a queued
    signal, so the GUI thread does the widget updates. The function must
    not touch widgets.

    on_finish, when given, is called in the worker thread after the
    function returns or raises, even if the window that started the
    job is gone: it is how the one-job lock is released. It must be
    thread-safe and must not touch widgets.
    """

    progress = Signal(str, object, str)
    done = Signal(object)
    error = Signal(object)

    def __init__(self, func, on_finish=None, parent=None):
        super().__init__(parent)
        self._func = func
        self._on_finish = on_finish
        self._cancel = threading.Event()
        self._thread = None

    def start(self):
        with _LIVE_LOCK:
            _LIVE_WORKERS.add(self)
        logger.info("Job started")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    @property
    def cancel_event(self):
        return self._cancel

    def _report(self, label, fraction, detail=""):
        if isValid(self):
            self.progress.emit(label, fraction, detail)

    def _run(self):
        try:
            result = self._func(self._report, self._cancel)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the GUI
            logger.info("Job failed: %s", type(exc).__name__)
            if isValid(self):
                self.error.emit(exc)
        else:
            logger.info("Job finished")
            if isValid(self):
                self.done.emit(result)
        finally:
            with _LIVE_LOCK:
                _LIVE_WORKERS.discard(self)
            if self._on_finish is not None:
                try:
                    self._on_finish()
                except Exception:  # noqa: BLE001 - never break the thread
                    pass

    def wait_finished(self, timeout_ms=5000):
        if self._thread is not None:
            self._thread.join(timeout_ms / 1000.0)


def shutdown_workers(timeout_ms=5000):
    """Cancel every running worker and wait for them to finish.

    Called before Qt goes away, so no worker thread is left emitting
    signals while the interpreter tears down.
    """
    with _LIVE_LOCK:
        workers = list(_LIVE_WORKERS)
    for worker in workers:
        worker.cancel()
    for worker in workers:
        worker.wait_finished(timeout_ms)
